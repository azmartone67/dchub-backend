"""The scheduler guards must SEE a module's own cron declaration — and must not
become vacuous in the process.

STEP ONE of decentralising _DISPATCH. Nothing declares CRON_JOBS yet, so every
consumer behaves exactly as before; what these pin is that the guards can no
longer be blindsided when the first declaration appears, and that teaching them
did not turn them into rubber stamps.

The vacuous version is the thing to fear. The obvious way to teach
scan_beat_scheduler_gaps about declarations is to concatenate the declaring
module's SOURCE into the text it already greps — but the module's own
`@bp.route(".../master-tick")` decorator would then satisfy the substring test
for EVERY module, so the guard would pass for everything forever. That is why
the collector AST-extracts only the CRON_JOBS assignment, and why
test_a_module_cannot_schedule_itself_by_merely_mentioning_the_path exists.
"""
import ast
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import cron_declarations as cd  # noqa: E402


def _tree(src):
    return ast.parse(src)


# ── extraction ────────────────────────────────────────────────────────

def test_extracts_a_declared_job():
    jobs, problems = cd._jobs_from_tree(_tree('''
CRON_JOBS = [{"label": "ghost_shell_daily",
              "path": "/api/v1/admin/ghost/master-tick",
              "method": "POST"}]
'''))
    assert jobs == [{"label": "ghost_shell_daily",
                     "path": "/api/v1/admin/ghost/master-tick"}]
    assert problems == []


def test_extracts_several():
    jobs, _ = cd._jobs_from_tree(_tree('''
CRON_JOBS = [{"label": "a", "path": "/p/a"}, {"label": "b", "path": "/p/b"}]
'''))
    assert {j["path"] for j in jobs} == {"/p/a", "/p/b"}


def test_a_module_with_no_declaration_yields_nothing():
    jobs, problems = cd._jobs_from_tree(_tree("X = 1\ndef f():\n    pass\n"))
    assert jobs == [] and problems == []


# ── the vacuity traps ─────────────────────────────────────────────────

def test_a_route_decorator_is_not_a_declaration():
    """THE trap. Mentioning the tick path anywhere in the module — which every
    shell does, in its own @bp.route — must NOT count as scheduling it."""
    jobs, _ = cd._jobs_from_tree(_tree('''
@bp.route("/api/v1/admin/ghost/master-tick", methods=["POST"])
def tick():
    return "ok"
'''))
    assert jobs == [], "a route decorator was mistaken for a schedule"


def test_a_path_in_a_docstring_or_comment_is_not_a_declaration():
    jobs, _ = cd._jobs_from_tree(_tree('''
"""This module ticks at /api/v1/admin/ghost/master-tick every hour."""
X = "/api/v1/admin/ghost/master-tick"
'''))
    assert jobs == [], "a string constant was mistaken for a declaration"


def test_a_nested_cron_jobs_is_not_module_level():
    """A CRON_JOBS built inside a function is not a declaration the scanner can
    verify statically, and must not be treated as one."""
    jobs, _ = cd._jobs_from_tree(_tree('''
def build():
    CRON_JOBS = [{"label": "x", "path": "/p/x"}]
    return CRON_JOBS
'''))
    assert jobs == []


# ── unverifiable declarations are REPORTED, never silently dropped ────

def test_a_computed_path_is_reported_not_guessed():
    """An f-string path cannot be resolved without importing. Guessing would let
    a guard believe a route is scheduled when the value is something else."""
    jobs, problems = cd._jobs_from_tree(_tree('''
CRON_JOBS = [{"label": "x", "path": f"{PREFIX}/master-tick"}]
'''))
    assert jobs == []
    assert any("non-literal" in p or "no literal" in p for p in problems), problems


def test_a_name_reference_is_reported():
    jobs, problems = cd._jobs_from_tree(_tree('''
TICK = "/p/x"
CRON_JOBS = [{"label": "x", "path": TICK}]
'''))
    assert jobs == [] and problems


def test_a_non_list_cron_jobs_is_reported():
    jobs, problems = cd._jobs_from_tree(_tree('CRON_JOBS = {"label": "x"}\n'))
    assert jobs == [] and any("not a list" in p for p in problems)


def test_an_entry_without_a_path_is_reported():
    jobs, problems = cd._jobs_from_tree(_tree('CRON_JOBS = [{"label": "x"}]\n'))
    assert jobs == [] and any("no literal 'path'" in p for p in problems)


# ── the collector never imports ───────────────────────────────────────

def test_collector_does_not_import_the_modules_it_reads():
    """Importing would run side effects in modules main.py may never import
    (routes/global_infra.py starts a daemon thread at import) and would let one
    ImportError take the whole scan down. Parsing is inert."""
    src = open(os.path.join(_ROOT, "routes", "cron_declarations.py"),
               encoding="utf-8").read()
    for banned in ("importlib", "__import__", "exec(", "eval(",
                   "ast.literal_eval", "runpy"):
        assert banned not in src, f"collector must not {banned}"
    assert "ast.parse" in src


def test_an_unparseable_module_does_not_break_the_scan(tmp_path):
    """One broken file must not take the scan down, and a good declaration
    beside it must still be found."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "broken.py").write_text("def (:\n")
    (tmp_path / "routes" / "good.py").write_text(
        'CRON_JOBS = [{"label": "g", "path": "/p/g"}]\n')
    assert cd.declared_paths(str(tmp_path)) == {"/p/g"}


def test_a_declaration_hidden_by_a_syntax_error_is_reported(tmp_path):
    """★ The case that matters: a module that DECLARES a job but does not parse.
    Its declaration is invisible to every scanner, so the module would look
    unscheduled with no explanation. That must be reported, not skipped.

    A broken module with NO declaration is correctly silent — it has nothing to
    lose — which is why the cheap 'CRON_JOBS' gate in collect_problems is a
    filter on relevance, not a hole."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "broken.py").write_text(
        'CRON_JOBS = [{"label": "g", "path": "/p/g"}]\ndef (:\n')
    probs = cd.collect_problems(str(tmp_path))
    assert any("unparseable" in p and "broken.py" in p for p in probs), probs
    assert cd.declared_paths(str(tmp_path)) == set()

    (tmp_path / "routes" / "quiet.py").write_text("def (:\n")   # no declaration
    probs2 = cd.collect_problems(str(tmp_path))
    assert not any("quiet.py" in p for p in probs2), (
        "a broken module with no declaration has nothing to lose and should "
        "not be reported as a declaration problem")


# ── the two guards, end to end ────────────────────────────────────────

def _fake_repo(tmp_path, *, declare_path=None, heartbeat_has=None):
    """A shell that declares _beat_ledger and a tick route, plus a
    cron_heartbeat.py that may or may not drive it."""
    (tmp_path / "routes").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    body = (
        'from flask import Blueprint\n'
        'bp = Blueprint("ghost", __name__)\n'
        'def _beat_ledger():\n    pass\n'
        '@bp.route("/api/v1/admin/ghost/master-tick", methods=["POST"])\n'
        'def tick():\n    return "ok"\n'
    )
    if declare_path:
        body += f'CRON_JOBS = [{{"label": "ghost_shell_daily", "path": "{declare_path}"}}]\n'
    (tmp_path / "routes" / "ghost_master_shell.py").write_text(body)
    (tmp_path / "routes" / "cron_heartbeat.py").write_text(
        f'_DISPATCH = [("other", f"{{BASE}}{heartbeat_has}", "POST", lambda n: True)]\n'
        if heartbeat_has else "_DISPATCH = []\n")
    return tmp_path


@pytest.fixture
def scanner():
    from routes import audit_closure_master_shell as m
    return m


def test_scanner_still_catches_an_unscheduled_beat(scanner, tmp_path):
    """MUST-FAIL control, unchanged in spirit: no declaration, no dispatch entry
    -> the gap must still be reported. If teaching the scanner about
    declarations silenced this, the guard would be a rubber stamp."""
    r = _fake_repo(tmp_path)
    gaps = scanner.scan_beat_scheduler_gaps(str(r))
    assert any("ghost_master_shell.py" in g for g in gaps), gaps


def test_scanner_accepts_a_module_that_declares_its_own_job(scanner, tmp_path):
    r = _fake_repo(tmp_path, declare_path="/api/v1/admin/ghost/master-tick")
    gaps = scanner.scan_beat_scheduler_gaps(str(r))
    assert not any("ghost_master_shell.py" in g for g in gaps), gaps


def test_a_module_cannot_schedule_itself_by_merely_mentioning_the_path(
        scanner, tmp_path):
    """★ THE VACUITY CONTROL. The module contains its tick path (in its own
    @bp.route) but declares NO CRON_JOBS. If the scanner had been taught by
    concatenating module source, this would pass and the guard would be dead."""
    r = _fake_repo(tmp_path)
    gaps = scanner.scan_beat_scheduler_gaps(str(r))
    assert any("ghost_master_shell.py" in g for g in gaps), (
        "the scanner accepted a module that only MENTIONS its tick path — "
        "it is matching source text, not declarations")


def test_declaring_a_different_path_does_not_schedule_the_tick(
        scanner, tmp_path):
    """Declaring SOME job must not vouch for an unrelated route."""
    r = _fake_repo(tmp_path, declare_path="/api/v1/admin/other/master-tick")
    gaps = scanner.scan_beat_scheduler_gaps(str(r))
    assert any("ghost_master_shell.py" in g for g in gaps), (
        "a declaration for a different path was accepted as scheduling this one")


# ── today's tree is unchanged ─────────────────────────────────────────

def test_nothing_declares_cron_jobs_yet():
    """Step one is capability only. If this starts failing, step two has begun —
    and the guards above are what make that safe."""
    assert cd.collect_declared_jobs(_ROOT) == []


def test_the_live_tree_has_no_unverifiable_declarations():
    assert cd.collect_problems(_ROOT) == []


# ── guard 2: the dead-man coverage check ──────────────────────────────
#
# This is the SILENT-GREEN guard — a decentralized shell dropping out of it
# does not go red, it just stops being watched. It had four controls fewer than
# guard 1 when this file was first written, which is exactly backwards: the
# quieter failure needs MORE proof, not less.

def _beat_repo(tmp_path, *, label, via, beats):
    """A repo with one shell, scheduled either through the _DISPATCH literal or
    its own CRON_JOBS, and either beating the dead-man ledger or not."""
    (tmp_path / "routes").mkdir(exist_ok=True)
    body = 'from flask import Blueprint\nbp = Blueprint("g", __name__)\n'
    if beats:
        body += 'def beat():\n    return "ingest-runs/beat"\n'
    if via == "declaration":
        body += f'CRON_JOBS = [{{"label": "{label}", "path": "/p/tick"}}]\n'
    (tmp_path / "routes" / "ghost_master_shell.py").write_text(body)
    dispatch = (f'_DISPATCH = [("{label}", f"{{BASE}}/p/tick", "POST", lambda n: True)]\n'
                if via == "literal" else "_DISPATCH = []\n")
    (tmp_path / "routes" / "cron_heartbeat.py").write_text(dispatch)
    return str(tmp_path)


@pytest.fixture
def beat_guard():
    sys.path.insert(0, os.path.join(_ROOT, "tests"))
    import test_shell_beat_reports_red as m
    return m


def test_ledger_guard_catches_an_unwatched_shell_on_the_literal(beat_guard, tmp_path):
    """MUST-FAIL control for the ORIGINAL path — proves teaching the guard about
    declarations did not blunt what it already caught."""
    r = _beat_repo(tmp_path, label="ghost_shell_daily", via="literal", beats=False)
    assert "ghost_shell_daily" in beat_guard._unwatched_shells(r)


def test_ledger_guard_catches_an_unwatched_shell_that_declared_itself(
        beat_guard, tmp_path):
    """★ THE POINT OF STEP ONE. A shell that schedules itself via CRON_JOBS and
    beats nothing must still be reported. Before the wiring, this returned an
    empty list — passing green while nothing watched the shell."""
    r = _beat_repo(tmp_path, label="ghost_shell_daily", via="declaration", beats=False)
    assert "ghost_shell_daily" in beat_guard._unwatched_shells(r), (
        "a self-declared shell with no dead-man feed was invisible to the "
        "coverage guard — this is the silent-green hole step one exists to close")


def test_ledger_guard_is_quiet_when_a_declared_shell_does_beat(beat_guard, tmp_path):
    """And it must not cry wolf: declared + beating is healthy."""
    r = _beat_repo(tmp_path, label="ghost_shell_daily", via="declaration", beats=True)
    assert beat_guard._unwatched_shells(r) == []


def test_declared_shell_labels_reach_the_scheduled_set(beat_guard, tmp_path):
    r = _beat_repo(tmp_path, label="ghost_shell_daily", via="declaration", beats=False)
    assert "ghost_shell_daily" in beat_guard._scheduled_shell_labels(r)


def test_a_declared_non_shell_job_is_not_treated_as_a_shell(beat_guard, tmp_path):
    """The guard is scoped to shells. A declared ordinary job must not be pulled
    into shell coverage and reported as an unwatched shell."""
    r = _beat_repo(tmp_path, label="ordinary_job_daily", via="declaration", beats=False)
    assert beat_guard._unwatched_shells(r) == []
