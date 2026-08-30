"""The failover drill must assert what the MIRROR SERVES, and must be armed.

WHY (measured 2026-08-30). A Railway deploy moved the edge onto Render for a
few minutes and https://dchub.cloud/.well-known/mcp_facts.json published a
30-day-old copy — HTTP 200, cf-cache-status DYNAMIC, valid JSON, plausible
numbers, `facilities` 21% understated. Nothing failed. Forced side by side:

    render   generated_at 2026-07-30T08:26:39Z   facilities 15,300+
    railway  generated_at 2026-08-30T09:08:39Z   facilities 19,500+

The canary was green through all of it, for three independent reasons:

  1. it read only `is_failover` and `commit` from the forced probe, never the
     `stale` / `data_age_hours` the freshness endpoint publishes;
  2. those fields would not have helped — both origins share the Neon DB, so
     Render reports `stale:false` with `data_age_hours: 0.14` while serving a
     month-old BUILD. The endpoint measures DATA age; the artifact is a FILE;
  3. the commit check asserts ANCESTRY, not recency, deliberately — "Render
     trails main by design". 164 commits behind is a true ancestor and passes.

So the artifact needs its own assertion, against the served bytes. These tests
pin that it exists, that it is ARMED, and that its parser survives the shape
the surface is actually served in.
"""

from __future__ import annotations

import os
import re
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "dchub-failover-check.sh")
_WORKFLOW = os.path.join(_ROOT, ".github", "workflows", "failover-canary.yml")


def _uncommented(path: str) -> str:
    """Source with whole-line comments removed.

    Six text-level checks in this repo have matched their OWN explanatory
    comment and passed while the code said the opposite. Every assertion below
    reads this, never the raw file.
    """
    keep = []
    for line in open(path, encoding="utf-8"):
        if line.lstrip().startswith("#"):
            continue
        keep.append(line)
    return "".join(keep)


# ── the assertion exists and is armed ───────────────────────────────────

def test_the_deep_drill_is_armed():
    """Kills: re-disarming to get a green board.

    It shipped ENFORCE=0 on 2026-07-30 with an in-file comment calling arming
    "a REQUIRED follow-up" after one clean week, and was still 0 thirty-one
    days later. A fence nobody armed is a fence that cannot fail.
    """
    wf = _uncommented(_WORKFLOW)
    assert "FAILOVER_DEEP_DRILL_ENFORCE: '1'" in wf, (
        "the deep drill is not armed — WOULD-FAIL output does not fail a run, "
        "so every assertion in it is decorative")
    assert "FAILOVER_DEEP_DRILL_ENFORCE: '0'" not in wf


def test_the_drill_asserts_the_served_artifact_not_only_a_proxy():
    """Kills: reverting to commit-ancestry alone, which is blind to staleness."""
    src = _uncommented(_SCRIPT)
    assert "ARTIFACT_URL=" in src, "no served-artifact probe at all"
    assert "mcp_facts.json" in src
    assert "X-DCHUB-Force-Backend: render" in src, (
        "the artifact must be fetched through the FORCED path — an unforced "
        "read hits the healthy primary and proves nothing about the mirror")
    assert "ARTIFACT_MAX_AGE_H" in src, "no freshness threshold"
    # the age must actually gate something, not merely be printed
    assert re.search(r"a_age_h\s*>\s*ARTIFACT_MAX_AGE_H", src), (
        "the computed age is not compared against the threshold")


def test_the_stale_verdict_is_fatal_and_names_the_fix():
    """A red that does not say what to do gets ignored — 17 days of daily red
    on mcp-facts-export were read as cosmetic for exactly that reason."""
    src = _uncommented(_SCRIPT)
    stale_branch = src[src.index("a_age_h > ARTIFACT_MAX_AGE_H"):]
    stale_branch = stale_branch[:stale_branch.index("else")]
    assert "note " in stale_branch, (
        "the stale case must go through note(), which is what respects ENFORCE")
    assert "autoDeploy" in stale_branch or "RENDER_DEPLOY_HOOK_URL" in stale_branch, (
        "the failure text must name the remediation")


# ── the parser survives the shape the surface is really served in ───────

def _run_parser(body: str) -> str:
    """Run the SCRIPT'S OWN extraction line, not a copy of it.

    A hand-copied pipeline in the test would keep passing after the real one
    broke, which is the whole failure mode under audit here.
    """
    src = open(_SCRIPT, encoding="utf-8").read()
    line = next(l for l in src.splitlines() if l.strip().startswith("a_gen="))
    prog = f'a_body="$1"\n{line}\nprintf "%s" "$a_gen"\n'
    out = subprocess.run(["bash", "-c", prog, "_", body],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def test_parser_reads_the_pretty_printed_shape_the_surface_actually_serves():
    """Kills the bug this guard was written with.

    The first draft grepped '"generated_at":"..."' with no whitespace, but the
    exporter writes json.dumps(indent=2), so the served bytes are
    '"generated_at": "..."'. Against the real mirror it matched nothing and the
    drill reported "cannot judge mirror freshness" — a guard failing for a
    reason unrelated to the thing it guards, which reads as a broken check
    rather than a broken surface.
    """
    body = ('{\n  "_generated_by": "dchub-backend/mcp_facts_export.py",\n'
            '  "generated_at": "2026-07-30T08:26:39Z",\n'
            '  "numbers": {\n    "facilities": "15,300+"\n  }\n}\n')
    assert _run_parser(body) == "2026-07-30T08:26:39Z"


def test_parser_also_reads_the_compact_shape():
    """Both shapes, so a formatting change on either side cannot blind it."""
    assert _run_parser('{"generated_at":"2026-08-30T09:08:39Z","numbers":{}}') \
        == "2026-08-30T09:08:39Z"


def test_parser_returns_empty_rather_than_guessing_when_the_field_is_absent():
    """Absent must be distinguishable from stale: the script reports
    'cannot judge' for the former and a hard verdict for the latter."""
    assert _run_parser('{"numbers":{"facilities":"19,500+"}}') == ""


# ── the alarm must stay one alarm ───────────────────────────────────────

def test_failure_notice_does_not_file_a_new_issue_every_run():
    """An armed drill against a genuinely broken mirror runs 4x a day. The old
    notify put a timestamp in the title, so it would have filed ~120 identical
    issues a month and trained everyone to scroll past them.

    ★ A first draft of this test inspected only the `--title` line, so moving
    the timestamp one line up into the `TITLE=` assignment defeated it — the
    mutation applied and the test stayed green. Scope the assertion to the
    WHOLE notify step, not to the line the value is finally used on: a check
    that reads one hop of a value is blind to the hop before it.
    """
    wf = _uncommented(_WORKFLOW)
    marker = "Notify on workflow failure"
    assert marker in wf, "the notify step is gone"
    notify = wf[wf.index(marker):]
    assert "$(date" not in notify, (
        "the failure notice builds a per-run value from the clock; anything "
        "time-varying reaching the issue TITLE files a new issue every run "
        "instead of reusing one:\n" + notify[:600])
    assert "gh issue list" in notify, (
        "nothing checks for an already-open issue before filing one")
