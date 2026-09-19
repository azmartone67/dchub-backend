"""Shape guards for .github/workflows/ci-neon-db.yml.

Every assertion here reads a PARSED YAML VALUE, never the file's text. The
workflow is heavily commented and several of those comments quote the very
expressions below; a text search would be satisfied by the prose explaining a
rule even after the rule itself was deleted.

Why these particular invariants: on 2026-09-18 the lane died with
`ROOT_BRANCHES_LIMIT_EXCEEDED` — four ci- root branches alive plus production
against a 5-root cap. Nothing had leaked. Two `if:` expressions were quietly
spending root slots on cron ticks, and nothing bounded concurrent holders.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

_WF = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/ci-neon-db.yml"


@pytest.fixture(scope="module")
def wf():
    doc = yaml.safe_load(_WF.read_text())
    # `on:` is YAML 1.1 truthy, so PyYAML gives back the boolean True as the key.
    doc["triggers"] = doc[True] if True in doc else doc["on"]
    return doc


def _creating_jobs(wf):
    """The jobs that cut a root branch — found by what they RUN, not by name."""
    found = {}
    for name, job in wf["jobs"].items():
        for step in job.get("steps", []):
            if "neon_ci_branch.py create" in str(step.get("run", "")):
                found[name] = job
    return found


def test_the_creating_jobs_are_the_two_we_think_they_are(wf):
    assert sorted(_creating_jobs(wf)) == ["ephemeral-db", "full-suite-measurement"]


def test_the_pr_lane_does_not_cut_a_branch_on_a_cron_tick(wf):
    """`github.event_name != 'pull_request'` is TRUE for a schedule event, so
    without this clause every sweeper tick also cut a parity branch."""
    cond = " ".join(str(wf["jobs"]["ephemeral-db"]["if"]).split())
    assert "github.event_name != 'schedule'" in cond


def test_the_full_suite_names_one_cron_and_that_cron_exists(wf):
    """Two crons are declared; `github.event_name` cannot tell them apart, so
    the 6-hourly sweeper tick was also starting the 40-minute suite.

    The second half of this test is the part that matters over time: this repo
    re-staggers cron expressions (tests/test_ops_cron_stagger_0902.py). A
    re-stagger that misses the `if:` would leave the job permanently unable to
    fire, which looks exactly like a job that simply never has work to do.
    """
    cond = " ".join(str(wf["jobs"]["full-suite-measurement"]["if"]).split())
    assert "github.event.schedule" in cond
    named = re.findall(r"github\.event\.schedule\s*==\s*'([^']+)'", cond)
    assert named, f"the full-suite job must name a cron, got: {cond}"
    declared = {c["cron"] for c in wf["triggers"]["schedule"]}
    assert set(named) <= declared, f"{named} not in declared crons {declared}"


def test_every_creating_job_is_handed_a_root_ceiling(wf):
    for name, job in _creating_jobs(wf).items():
        assert "NEON_CI_MAX_ROOTS" in (job.get("env") or {}), name


def test_every_creating_job_destroys_under_always(wf):
    """The cheapest of the three cleanup layers, and the one a rename breaks."""
    for name, job in _creating_jobs(wf).items():
        destroys = [s for s in job["steps"]
                    if "neon_ci_branch.py destroy" in str(s.get("run", ""))]
        assert len(destroys) == 1, name
        assert "always()" in str(destroys[0].get("if", "")), name


def test_every_creating_job_sets_a_ttl_so_expires_at_is_armed(wf):
    for name, job in _creating_jobs(wf).items():
        create = next(s for s in job["steps"]
                      if "neon_ci_branch.py create" in str(s.get("run", "")))
        assert "--ttl-hours" in str(create["run"]), name


def test_the_sweeper_cannot_delete_a_branch_a_running_job_still_holds(wf):
    """The sweeper's age cutoff must sit ABOVE the longest job it runs beside,
    or it reclaims a database out from under a live test run."""
    sweep = next(s for j in wf["jobs"].values() for s in j.get("steps", [])
                 if "neon_ci_branch.py sweep" in str(s.get("run", "")))
    hours = int(re.search(r"--ttl-hours\s+(\d+)", str(sweep["run"])).group(1))
    longest = max(j["timeout-minutes"] for j in _creating_jobs(wf).values())
    assert hours * 60 > longest, f"sweep cutoff {hours}h <= job budget {longest}m"


def test_the_creating_jobs_leave_room_for_the_wait_they_may_do(wf):
    """`create` can block for NEON_CI_WAIT_MINUTES (default 8) before it does
    any work. A job budget that ignores the queue turns a wait into a timeout."""
    for name, job in _creating_jobs(wf).items():
        assert job["timeout-minutes"] >= 30, name


# ── the UNMEASURED gate ──────────────────────────────────────────────────────


def _db_steps(job):
    """Steps between `create` and `destroy` — the ones that need a database.

    Bounded by position, not by name, so a step ADDED here later is caught by
    the gate test below instead of quietly running without a database.
    """
    steps = job.get("steps", [])
    start = next(i for i, s in enumerate(steps)
                 if "neon_ci_branch.py create" in str(s.get("run", "")))
    end = next(i for i, s in enumerate(steps)
               if "neon_ci_branch.py destroy" in str(s.get("run", "")))
    return steps[start + 1:end]


def test_every_database_step_is_gated_on_the_slot(wf):
    """★ The load-bearing half of exiting 0 when no root slot is free.

    `create` now reports starvation as UNMEASURED and hands back no DSN. Any
    step between create and destroy that is NOT gated would then run against an
    empty `NEON_CI_DSN` — which fails confusingly at best, and at worst runs a
    lane that quietly finds nothing and reports success.
    """
    for name, job in _creating_jobs(wf).items():
        steps = _db_steps(job)
        assert steps, name
        for s in steps:
            assert "slot != 'none'" in str(s.get("if", "")), (
                f"{name}: step {s.get('name')!r} runs without a database when "
                f"the lane is starved — gate it on steps.neon.outputs.slot")


def test_the_gate_reads_the_step_that_actually_creates_the_branch(wf):
    """A gate pointing at the wrong step id is always-true, so the whole thing
    is decoration. The id in the gate must be the id on the create step."""
    for name, job in _creating_jobs(wf).items():
        create = next(s for s in job["steps"]
                      if "neon_ci_branch.py create" in str(s.get("run", "")))
        sid = create.get("id")
        assert sid, f"{name}: the create step has no id for the gate to read"
        for s in _db_steps(job):
            assert f"steps.{sid}.outputs.slot" in str(s["if"]), (
                f"{name}: {s.get('name')!r} gates on a different step than the "
                f"one that creates the branch")


def test_destroy_still_runs_even_though_nothing_was_created(wf):
    """Starvation means no branch_id, so destroy must self-skip on the value
    rather than on the slot — otherwise a real branch leaks whenever a later
    change flips the gate."""
    for name, job in _creating_jobs(wf).items():
        d = next(s for s in job["steps"]
                 if "neon_ci_branch.py destroy" in str(s.get("run", "")))
        cond = str(d.get("if", ""))
        assert "always()" in cond and "branch_id != ''" in cond, name
# ── the ceiling has to be ONE number ─────────────────────────────────────────


_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts/neon_ci_branch.py"


def _workflow_fallbacks(wf):
    """Each creating job's ceiling for when the repo variable is unset.

    Read off the PARSED env value and matched against the `vars.` expression,
    so a comment mentioning the variable cannot stand in for the expression.
    """
    out = {}
    for name, job in _creating_jobs(wf).items():
        expr = str((job.get("env") or {}).get("NEON_CI_MAX_ROOTS") or "")
        m = re.search(r"vars\.NEON_CI_MAX_ROOTS\s*\|\|\s*'(\d+)'", expr)
        assert m, f"{name}: no `vars.NEON_CI_MAX_ROOTS || '<n>'` in {expr!r}"
        out[name] = int(m.group(1))
    return out


def _script_fallback():
    """`--max-roots`' own default, read from the AST.

    Not by import (the module body would run) and not by text match (the help
    string carries the same words). This is the argument's real default.
    """
    found = []
    for node in ast.walk(ast.parse(_SCRIPT.read_text())):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        head, *tail = node.values
        if "NEON_CI_MAX_ROOTS" not in ast.dump(head):
            continue
        if len(tail) == 1 and isinstance(tail[0], ast.Constant):
            found.append(tail[0].value)
    assert found, f"no `NEON_CI_MAX_ROOTS or <n>` default in {_SCRIPT.name}"
    assert len(set(found)) == 1, f"{_SCRIPT.name} disagrees with itself: {found}"
    return int(found[0])


def test_workflow_fallback_matches_the_script_default(wf):
    """#4809 raised the script default and left the workflow behind.

    The workflow always exports NEON_CI_MAX_ROOTS, so the script's own default
    is unreachable in CI and the two can drift for weeks without a symptom —
    until the repo variable is deleted and the lane silently changes capacity.
    """
    ceilings = _workflow_fallbacks(wf)
    # Floor: both creating jobs, measured 2026-09-19. Without it a broken
    # `_creating_jobs` would make this pass by finding nothing to check.
    assert len(ceilings) >= 2, f"expected >=2 creating jobs, got {ceilings}"
    script = _script_fallback()
    assert set(ceilings.values()) == {script}, (
        f"workflow fallbacks {ceilings} vs {_SCRIPT.name} default {script}")


# ── the TTL has to outlive the job, and only one number may govern it ────────

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts/neon_ci_branch.py"


def _create_ttls(wf):
    """The --ttl-hours each creating job actually passes."""
    out = {}
    for name, job in _creating_jobs(wf).items():
        create = next(s for s in job["steps"]
                      if "neon_ci_branch.py create" in str(s.get("run", "")))
        m = re.search(r"--ttl-hours\s+(\d+)", str(create["run"]))
        assert m, f"{name} passes no --ttl-hours value"
        out[name] = int(m.group(1))
    return out


def _script_ttl_defaults():
    """`--ttl-hours` argparse defaults, keyed by the parser they are added to.

    Read from the AST, not the text: the file's comments quote these numbers.
    """
    found = {}
    for node in ast.walk(ast.parse(_SCRIPT.read_text())):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and getattr(node.args[0], "value", None) == "--ttl-hours"):
            for kw in node.keywords:
                if kw.arg == "default":
                    found[getattr(node.func.value, "id", "?")] = kw.value.value
    return found


def test_the_branch_ttl_outlives_the_job_that_holds_it(wf):
    """`expires_at` is enforced NEON-side: it deletes the branch whether or not
    a job is still using it. A TTL at or below the job budget is a database
    vanishing mid-run — the opposite failure from the leak the TTL exists for,
    and the one a well-meant reduction introduces.
    """
    for name, hours in _create_ttls(wf).items():
        budget = wf["jobs"][name]["timeout-minutes"]
        assert hours * 60 > budget, f"{name}: ttl {hours}h <= job budget {budget}m"


def test_the_creating_jobs_agree_on_one_ttl(wf):
    ttls = set(_create_ttls(wf).values())
    assert len(ttls) == 1, f"creating jobs disagree on the TTL: {ttls}"


def test_the_workflow_ttl_matches_the_script_default(wf):
    """Two places name this number and the workflow's wins, because it is
    passed explicitly. Editing only the argparse default is a no-op in CI, so
    they are pinned together rather than left to drift apart silently.
    """
    defaults = _script_ttl_defaults()
    assert defaults, "no --ttl-hours default found in the script: guard is blind"
    assert "c" in defaults, f"create parser not found; got {sorted(defaults)}"
    assert set(_create_ttls(wf).values()) == {defaults["c"]}, (
        f"workflow passes {sorted(set(_create_ttls(wf).values()))}, "
        f"script default is {defaults['c']}")
