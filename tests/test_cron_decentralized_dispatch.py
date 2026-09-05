"""A module may schedule its own cron job — and every consumer must honour it.

STEP TWO. Step one taught the two scheduler GUARDS to see declarations; this
makes the dispatcher actually run them, so a feature stops having to append to
_DISPATCH (and to _HEAVY_LABELS, and to _MIN_REFIRE_S) in one shared file.

Four consumers had to change together, and a declaration honoured by three of
them is a bug that only shows up in production:
    the dispatch loop, _refire_suppressed, the heavy/light splitter,
    and the counts on /api/v1/cron/health.

★ The declaration is read from sys.modules, never imported here. The predicate
is a lambda, so it cannot be read statically — but importing route modules from
inside the dispatcher would put all 120 jobs one ImportError away from
disappearing. main.py has already imported every module whose blueprint
registered; this reads what is already there.

★ That absence must not be silent, which is what declaration_drift() is for and
why step one shipped first: the static read off disk is the oracle for what
SHOULD be live.
"""
import os
import sys
import types

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from routes import cron_heartbeat as ch  # noqa: E402


@pytest.fixture
def declare(monkeypatch):
    """Install a fake already-imported routes module carrying CRON_JOBS."""
    made = []

    def _make(jobs, name="routes.fake_ghost_shell"):
        mod = types.ModuleType(name)
        mod.CRON_JOBS = jobs
        monkeypatch.setitem(sys.modules, name, mod)
        made.append(name)
        return mod
    return _make


ALWAYS = (lambda now: True)
NEVER = (lambda now: False)


def _job(**kw):
    base = {"label": "ghost_shell_daily", "path": "/api/v1/admin/ghost/master-tick",
            "method": "POST", "when": ALWAYS}
    base.update(kw)
    return base


# ── today's tree is unchanged ─────────────────────────────────────────

def test_with_no_declarations_the_dispatch_is_exactly_the_literal():
    """Step two must be invisible until something declares. If this fails, the
    collector is inventing jobs."""
    assert ch._declared_jobs_live() == []
    assert len(ch._effective_dispatch()) == len(ch._DISPATCH)


def test_heavy_and_refire_tables_are_unchanged_with_no_declarations():
    assert ch._heavy_labels() == ch._HEAVY_LABELS
    assert ch._min_refire_table() == ch._MIN_REFIRE_S


# ── consumer 1: the dispatch loop ─────────────────────────────────────

def test_a_declared_job_reaches_the_effective_dispatch(declare):
    declare([_job()])
    labels = [t[0] for t in ch._effective_dispatch()]
    assert "ghost_shell_daily" in labels


def test_the_collector_joins_BASE_so_a_module_declares_a_PATH(declare):
    declare([_job()])
    entry = next(t for t in ch._effective_dispatch() if t[0] == "ghost_shell_daily")
    assert entry[1] == f"{ch.BASE}/api/v1/admin/ghost/master-tick", (
        "a declaring module must not have to know BASE")


def test_the_declared_predicate_is_the_one_that_runs(declare):
    declare([_job(when=NEVER)])
    entry = next(t for t in ch._effective_dispatch() if t[0] == "ghost_shell_daily")
    assert entry[3](None) is False, "the module's own predicate was not used"


def test_method_defaults_to_post_and_is_upcased(declare):
    declare([_job(method="get")])
    entry = next(t for t in ch._effective_dispatch() if t[0] == "ghost_shell_daily")
    assert entry[2] == "GET"
    declare([{"label": "m2", "path": "/p", "when": ALWAYS}], name="routes.fake_m2")
    e2 = next(t for t in ch._effective_dispatch() if t[0] == "m2")
    assert e2[2] == "POST"


# ── consumer 2: the heavy/light splitter ──────────────────────────────

def test_declared_heavy_reaches_the_heavy_set(declare):
    declare([_job(heavy=True)])
    assert "ghost_shell_daily" in ch._heavy_labels()


def test_a_declaration_without_heavy_is_light(declare):
    declare([_job()])
    assert "ghost_shell_daily" not in ch._heavy_labels()


def test_declared_heavy_does_not_disturb_the_static_set(declare):
    declare([_job(heavy=True)])
    assert ch._HEAVY_LABELS <= ch._heavy_labels(), "static heavy labels were lost"


# ── consumer 3: re-fire suppression ───────────────────────────────────

def test_declared_min_refire_reaches_the_table(declare):
    declare([_job(min_refire_s=3600)])
    assert ch._min_refire_table().get("ghost_shell_daily") == 3600


def test_declared_min_refire_does_not_drop_static_entries(declare):
    declare([_job(min_refire_s=3600)])
    table = ch._min_refire_table()
    for k, v in ch._MIN_REFIRE_S.items():
        assert table[k] == v, f"static re-fire window for {k} was lost"


# ── malformed declarations degrade, never crash ───────────────────────

@pytest.mark.parametrize("bad", [
    {"label": "x", "path": "/p"},                       # no predicate
    {"label": "x", "when": ALWAYS},                     # no path
    {"path": "/p", "when": ALWAYS},                     # no label
    {"label": "x", "path": "/p", "when": "not callable"},
    "not a dict",
])
def test_a_malformed_declaration_is_dropped_not_raised(declare, bad):
    declare([bad])
    ch._effective_dispatch()          # must not raise
    assert all(t[0] != "x" for t in ch._effective_dispatch())


def test_a_non_list_cron_jobs_is_ignored(declare):
    declare({"label": "x"})
    assert ch._declared_jobs_live() == []


# ── the collector must not import ─────────────────────────────────────

def test_collector_reads_sys_modules_and_imports_nothing():
    """Importing route modules from the dispatcher would put all 120 jobs one
    ImportError away from disappearing — main.py wraps this module's own import
    in a blanket try/except that prints and continues."""
    import inspect
    src = inspect.getsource(ch._declared_jobs_live)
    assert "sys.modules" in src
    for banned in ("importlib", "__import__(", "exec(", "eval("):
        assert banned not in src, f"the collector must not {banned}"


def test_a_module_absent_from_sys_modules_contributes_nothing():
    assert ch._declared_jobs_live() == []


# ── the silent death is closed ────────────────────────────────────────

def test_drift_reports_a_declaration_that_did_not_materialize(monkeypatch, tmp_path):
    """★ THE POINT OF STEP ONE BEING FIRST. A module that fails to import loses
    its job silently: main.py swallows the registration error, so nothing is
    live and nothing complains. The static read off disk is the oracle."""
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "ghost_master_shell.py").write_text(
        'CRON_JOBS = [{"label": "ghost_shell_daily", "path": "/p/tick"}]\n')
    monkeypatch.setattr(
        ch, "_declared_jobs_live", lambda: [])        # nothing imported
    import routes.cron_declarations as cd
    monkeypatch.setattr(
        cd, "collect_declared_jobs",
        lambda root: [{"label": "ghost_shell_daily", "path": "/p/tick",
                       "module": "routes/ghost_master_shell.py"}])
    monkeypatch.setattr(cd, "collect_problems", lambda root: [])
    drift = ch.declaration_drift()
    assert any("ghost_shell_daily" in d for d in drift), drift
    assert any("not live" in d for d in drift), drift


def test_drift_is_empty_when_disk_and_runtime_agree(monkeypatch):
    import routes.cron_declarations as cd
    monkeypatch.setattr(cd, "collect_declared_jobs", lambda root: [])
    monkeypatch.setattr(cd, "collect_problems", lambda root: [])
    assert ch.declaration_drift() == []


def test_drift_is_unmeasurable_loudly_never_silently(monkeypatch):
    """If the oracle itself breaks, that must be visible — an empty list would
    read as 'no drift' and be a lie."""
    import routes.cron_declarations as cd
    def boom(root):
        raise RuntimeError("oracle down")
    monkeypatch.setattr(cd, "collect_declared_jobs", boom)
    out = ch.declaration_drift()
    assert out and "unmeasurable" in out[0], out


def test_drift_surfaces_unverifiable_disk_declarations(monkeypatch):
    """A CRON_JOBS whose path is computed cannot be checked against runtime, so
    it rides out on the same channel rather than being dropped."""
    import routes.cron_declarations as cd
    monkeypatch.setattr(cd, "collect_declared_jobs", lambda root: [])
    monkeypatch.setattr(
        cd, "collect_problems",
        lambda root: ["routes/x.py: CRON_JOBS entry has a non-literal 'path'"])
    assert any("non-literal" in d for d in ch.declaration_drift())

# ═══════════════════════════════════════════════════════════════════════
# BEHAVIOURAL: the CONSUMERS must call the accessors
#
# ★ The tests above assert the accessor functions return the right thing. That
# is NOT the same as the dispatcher USING them, and mutation testing proved it:
# reverting the dispatch loop to `_DISPATCH`, the splitter to `_HEAVY_LABELS`
# and the suppressor to `_MIN_REFIRE_S` left all 23 of them GREEN. Three of four
# mutations survived — a mirror of the implementation, not a test of it.
#
# These drive the real code paths instead.
# ═══════════════════════════════════════════════════════════════════════

import datetime as _dt


@pytest.fixture
def app_client(monkeypatch):
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(ch.cron_heartbeat_bp)
    # Never let a test make a real request out of the process.
    monkeypatch.setattr(ch, "_hit", lambda *a, **k: {"status": 200, "outcome": "ok"})
    return app.test_client()


class _CapturingThread:
    """Captures the `due` list the dispatcher hands to its worker thread, and
    never starts it."""
    last_due = None

    def __init__(self, target=None, args=(), daemon=None, **kw):
        _CapturingThread.last_due = list(args[0]) if args else []
        self._target, self._args = target, args

    def start(self):
        pass


def test_the_dispatch_LOOP_fires_a_declared_job(app_client, declare, monkeypatch):
    """★ Reverting the loop to `_DISPATCH` must red THIS. Drives the real
    heartbeat handler and reads the job list it hands to its worker."""
    declare([_job(when=ALWAYS)])
    monkeypatch.setattr(ch.threading, "Thread", _CapturingThread)
    r = app_client.post("/api/v1/cron/heartbeat")
    assert r.status_code == 200
    labels = [j[0] for j in (_CapturingThread.last_due or [])]
    assert "ghost_shell_daily" in labels, (
        "the declared job never reached the dispatcher — the loop is still "
        f"reading the _DISPATCH literal. due={labels[:8]}")


def test_the_dispatch_LOOP_honours_a_declared_false_predicate(
        app_client, declare, monkeypatch):
    declare([_job(when=NEVER)])
    monkeypatch.setattr(ch.threading, "Thread", _CapturingThread)
    r = app_client.post("/api/v1/cron/heartbeat")
    assert "ghost_shell_daily" in (r.get_json() or {}).get("skipped", [])


def test_the_SPLITTER_puts_a_declared_heavy_job_in_the_heavy_batch(
        app_client, declare, monkeypatch):
    """★ Reverting the splitter to `_HEAVY_LABELS` must red THIS. Runs the real
    dispatch body and records which width each batch was submitted at."""
    declare([_job(when=ALWAYS, heavy=True)])
    seen = []

    def fake_run_batch(batch, width, hit=None):
        seen.append((width, [j[0] for j in batch]))
        return []

    monkeypatch.setattr(ch, "_run_batch", fake_run_batch)

    class _InlineThread:                       # run the worker body inline
        def __init__(self, target=None, args=(), daemon=None, **kw):
            self._t, self._a = target, args
        def start(self):
            self._t(*self._a)

    monkeypatch.setattr(ch.threading, "Thread", _InlineThread)
    app_client.post("/api/v1/cron/heartbeat")

    heavy_batches = [labels for width, labels in seen if width == 3]
    light_batches = [labels for width, labels in seen if width == 8]
    assert any("ghost_shell_daily" in b for b in heavy_batches), (
        "a job declaring heavy=True was not throttled — the splitter is still "
        f"reading the static _HEAVY_LABELS. batches={seen}")
    assert not any("ghost_shell_daily" in b for b in light_batches)


def test_the_SUPPRESSOR_honours_a_declared_min_refire_s(declare, monkeypatch):
    """★ Reverting the suppressor to `_MIN_REFIRE_S` must red THIS. Calls the
    real _refire_suppressed twice inside the declared window."""
    declare([_job(min_refire_s=3600)])
    monkeypatch.setattr(ch, "_LAST_FIRED", {})
    t0 = _dt.datetime(2026, 9, 4, 12, 0, 0)
    assert ch._refire_suppressed("ghost_shell_daily", t0) is False, "first fire"
    assert ch._refire_suppressed(
        "ghost_shell_daily", t0 + _dt.timedelta(minutes=5)) is True, (
        "a re-fire inside the DECLARED window was not suppressed — the "
        "suppressor is still reading the static _MIN_REFIRE_S")


def test_the_SUPPRESSOR_lets_a_fire_through_after_the_declared_window(
        declare, monkeypatch):
    declare([_job(min_refire_s=60)])
    monkeypatch.setattr(ch, "_LAST_FIRED", {})
    t0 = _dt.datetime(2026, 9, 4, 12, 0, 0)
    assert ch._refire_suppressed("ghost_shell_daily", t0) is False
    assert ch._refire_suppressed(
        "ghost_shell_daily", t0 + _dt.timedelta(seconds=120)) is False


def test_health_counts_include_declared_jobs(app_client, declare):
    declare([_job()])
    body = app_client.get("/api/v1/cron/health").get_json() or {}
    assert body.get("declared_jobs") == 1
    assert body.get("dispatch_count") == len(ch._DISPATCH) + 1
