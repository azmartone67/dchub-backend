"""backend-autonomous-brain beats what its cycle committed, or None.

The cycle's source-registry heartbeat read results["total_new_rows"], a key no
code produces, so every beat published rows_affected=0 whatever the cycle
inserted (20/20 public runs on 2026-09-13). These tests run the real cycle with
its steps returning the real extractors' result shapes (read from the source,
so a renamed counter fails here instead of drifting), and record the heartbeat
CALL where the cycle resolves it: `from dchub_heartbeat import heartbeat` runs
at call time, so it reads the attribute on sys.modules["dchub_heartbeat"].
"""
import ast
import importlib
import inspect
import pathlib
import sys

import pytest

SOURCE_ID = "backend-autonomous-brain"
# Exception text. The registry serves error text publicly, so no beat may carry it.
SENTINEL = "SENTINEL-MESSAGE-TEXT-7f3a"
REAL = object()  # _run(): keep this step's real method


def _no_db(*_a, **_k):
    raise RuntimeError("no database in this test")


@pytest.fixture
def brain_mod(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    if "autonomous_brain" not in sys.modules:
        # Importing builds a module-level AutonomousBrain whose state load
        # retries a missing database for ~16 s. Fail that fast, then give the
        # module its real get_db back.
        import db_utils
        real_get_db = db_utils.get_db
        monkeypatch.setattr(db_utils, "get_db", _no_db)
        importlib.import_module("autonomous_brain").get_db = real_get_db
    mod = sys.modules["autonomous_brain"]
    assert pathlib.Path(mod.__file__).name == "autonomous_brain.py", mod
    return mod


@pytest.fixture
def beats(monkeypatch):
    hb = importlib.import_module("dchub_heartbeat")
    assert pathlib.Path(hb.__file__).name == "dchub_heartbeat.py", hb
    calls = []

    def record(source_id, status="success", rows_affected=None,
               duration_ms=None, error=None, metadata=None):
        calls.append({"source_id": source_id, "status": status,
                      "rows_affected": rows_affected,
                      "duration_ms": duration_ms, "error": error})
        return True

    # The real parameters exactly, so a call the real function would reject
    # with TypeError is rejected here too.
    assert inspect.signature(record) == inspect.signature(hb.heartbeat)
    monkeypatch.setattr(sys.modules["dchub_heartbeat"], "heartbeat", record)
    return calls


@pytest.fixture
def brain(brain_mod, monkeypatch):
    monkeypatch.setattr(brain_mod, "get_db", _no_db)
    b = brain_mod.AutonomousBrain()
    b.state["total_cycles"] = 1  # not a 10th cycle, so no infrastructure sync
    monkeypatch.setattr(b, "_has_new_announcements", lambda: (True, 42))
    for name in ("_save_state", "_save_last_processed_announcement",
                 "_record_cycle_to_extraction_intelligence"):
        monkeypatch.setattr(b, name, lambda *a, **k: None)
    return b


def _methods(mod):
    tree = ast.parse(pathlib.Path(mod.__file__).read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "AutonomousBrain")
    return tree, {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}


def _results_subscript(node, key=None):
    """`results['<constant>']`, and that constant if `key` is given."""
    return (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id == "results" and isinstance(node.slice, ast.Constant)
            and (key is None or node.slice.value == key))


def _steps(mod):
    """{results key: method} for each `results['k'] = self.m()` in the cycle."""
    _, methods = _methods(mod)
    return {n.targets[0].slice.value: n.value.func.attr
            for n in ast.walk(methods["_run_autonomous_cycle"])
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and _results_subscript(n.targets[0])
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Attribute)
            and isinstance(n.value.func.value, ast.Name)
            and n.value.func.value.id == "self"}


def _shape(mod, step, **counts):
    """The step's initial `results = {...}` literal, with `counts` applied.

    Only keys that literal has, plus 'error' (set by the extractors' own except
    blocks), so a fake cannot report a counter the real step does not keep.
    """
    _, methods = _methods(mod)
    method = methods[_steps(mod).get(step, step)]
    init = next(n.value for n in method.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "results"
                        for t in n.targets))
    shape = ast.literal_eval(init)
    for key, value in counts.items():
        assert key in shape or key == "error", f"{step} results has no {key!r}"
        shape[key] = value
    return shape


def _run(mod, brain, monkeypatch, **outcomes):
    """Run the real cycle. A step returns its outcome if that is a dict, raises
    it if it is an exception, keeps its real method for REAL, and otherwise
    returns its all-zero initial shape."""
    for key, method in _steps(mod).items():
        outcome = outcomes.pop(key, None)
        if outcome is REAL:
            continue
        if outcome is None:
            outcome = _shape(mod, key)
        if isinstance(outcome, BaseException):
            def raiser(exc=outcome):
                raise exc
            monkeypatch.setattr(brain, method, raiser)
        else:
            monkeypatch.setattr(brain, method, lambda value=outcome: value)
    assert not outcomes, f"not steps of the cycle: {sorted(outcomes)}"
    return brain.run_autonomous_cycle()


def _verdicts(beats):
    return [(b["source_id"], b["status"], b["rows_affected"], b["error"])
            for b in beats]


def test_beat_sends_the_sum_of_the_committed_insert_tallies(
        brain_mod, brain, beats, monkeypatch):
    # Distinct tallies beside bigger pattern-match buckets: reading the missing
    # key (0), summing a bucket, or dropping a writer each gives another number.
    m = brain_mod
    _run(m, brain, monkeypatch,
         capacity=_shape(m, "capacity", new_pipeline=2, extracted=1000),
         deals=_shape(m, "deals", deals_found=3, scanned=900),
         gas_infrastructure=_shape(m, "gas_infrastructure", added=5,
                                   pipelines=100, midstream=200, lng=300),
         # transmission's 'added' is no committed-insert tally since GUARD #3
         # (2026-09-23): a 7 there must not reach the sum.
         transmission_infrastructure=_shape(
             m, "transmission_infrastructure", added=7,
             transmission_lines=400, hvdc=500),
         fiber_infrastructure=_shape(m, "fiber_infrastructure", dark_fiber=11,
                                     lit_fiber=13, carriers=17),
         infrastructure=_shape(m, "infrastructure", fiber_mentions=19),
         quality=_shape(m, "quality", fixed=29))
    assert _verdicts(beats) == [(SOURCE_ID, "success", 10, None)]
    assert isinstance(beats[0]["duration_ms"], int)


def test_a_cycle_that_added_nothing_beats_zero_not_none(
        brain_mod, brain, beats, monkeypatch):
    _run(brain_mod, brain, monkeypatch)
    assert _verdicts(beats) == [(SOURCE_ID, "success", 0, None)]


def test_a_writer_that_recorded_an_error_leaves_rows_unmeasured(
        brain_mod, brain, beats, monkeypatch):
    # deals counts its INSERTs before its one commit: these 3 never committed.
    m = brain_mod
    _run(m, brain, monkeypatch,
         deals=_shape(m, "deals", deals_found=3, error=SENTINEL),
         gas_infrastructure=_shape(m, "gas_infrastructure", added=5))
    assert _verdicts(beats) == [(SOURCE_ID, "partial", None, "steps failed: deals")]


def test_a_writer_that_raised_leaves_rows_unmeasured(
        brain_mod, brain, beats, monkeypatch):
    m = brain_mod
    _run(m, brain, monkeypatch,
         capacity=_shape(m, "capacity", new_pipeline=2),
         gas_infrastructure=RuntimeError(SENTINEL))
    assert _verdicts(beats) == [
        (SOURCE_ID, "partial", None, "steps failed: gas_infrastructure")]


def test_a_failed_step_that_writes_no_rows_keeps_the_count(
        brain_mod, brain, beats, monkeypatch):
    m = brain_mod
    _run(m, brain, monkeypatch,
         capacity=_shape(m, "capacity", new_pipeline=2),
         quality=RuntimeError(SENTINEL),
         infrastructure=_shape(m, "infrastructure", error=SENTINEL))
    assert _verdicts(beats) == [
        (SOURCE_ID, "partial", 2, "steps failed: infrastructure, quality")]


def test_every_writer_failing_is_a_failure(brain_mod, brain, beats, monkeypatch):
    m = brain_mod
    _run(m, brain, monkeypatch,
         capacity=_shape(m, "capacity", error=SENTINEL),
         deals=_shape(m, "deals", error=SENTINEL),
         gas_infrastructure=RuntimeError(SENTINEL),
         transmission_infrastructure=_shape(
             m, "transmission_infrastructure", error=SENTINEL))
    assert _verdicts(beats) == [(
        SOURCE_ID, "failure", None,
        "steps failed: capacity, deals, gas_infrastructure, "
        "transmission_infrastructure")]


class _Cursor:
    """What the row-writing extractors use of db_utils' cursor wrapper."""

    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def execute(self, sql, params=None):
        if "FROM announcements" in sql:
            self.rows = [self.conn.article]
        elif self.conn.insert_sql in sql:
            if not self.conn.insert_ok:
                raise RuntimeError(SENTINEL)
            self.rows = [(1,)]  # RETURNING 1: a row was written
        else:
            raise AssertionError(f"unexpected SQL: {sql.strip()[:60]}")

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def close(self):
        pass


class _Conn:
    """What the row-writing extractors use of db_utils' pooled connection."""

    def __init__(self, insert_sql, title, insert_ok):
        self.insert_sql, self.insert_ok, self.rollbacks = insert_sql, insert_ok, 0
        self.article = {"id": 1, "title": title, "content": "",
                        "source_url": "https://example.com/article"}

    def cursor(self, cursor_factory=None):
        return _Cursor(self)

    def commit(self):
        pass

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


# step: (the INSERT it issues, a title its patterns match, the bucket that fills)
_WRITERS = {
    "gas_infrastructure": ("INSERT INTO gas_pipelines",
                           "Acme to build 120 mile gas pipeline", "pipelines"),
}


@pytest.mark.parametrize("step", sorted(_WRITERS))
@pytest.mark.parametrize("insert_ok", [True, False], ids=["accepted", "rejected"])
def test_a_rejected_insert_is_not_a_cycle_that_found_nothing(
        brain_mod, brain, beats, monkeypatch, step, insert_ok):
    insert_sql, title, bucket = _WRITERS[step]
    conn = _Conn(insert_sql, title, insert_ok)
    monkeypatch.setattr(brain_mod, "get_db", lambda: conn)
    result = _run(brain_mod, brain, monkeypatch, **{step: REAL})[step]
    assert result[bucket] == 1 and "error" not in result, result  # the article matched
    if insert_ok:  # control: the same article with the INSERT accepted
        assert (result["added"], result.get("insert_errors"), conn.rollbacks) == (1, None, 0)
        assert _verdicts(beats) == [(SOURCE_ID, "success", 1, None)]
    else:
        assert (result["added"], result.get("insert_errors"), conn.rollbacks) == (0, 1, 1)
        assert _verdicts(beats) == [(SOURCE_ID, "partial", None, f"steps failed: {step}")]


def test_a_transmission_headline_is_counted_not_written(
        brain_mod, brain, beats, monkeypatch):
    # GUARD #3 (2026-09-23): the step wrote matching headlines into
    # transmission_lines. The fake cursor raises on any SQL but the announcements
    # read, and the step records that as 'error' — so an INSERT of any shape,
    # into any table, fails this test.
    conn = _Conn("<no write is expected>", "Grid Co plans 345 kV transmission line", True)
    monkeypatch.setattr(brain_mod, "get_db", lambda: conn)
    result = _run(brain_mod, brain, monkeypatch,
                  transmission_infrastructure=REAL)["transmission_infrastructure"]
    assert result["transmission_lines"] == 1, result  # control: the article matched
    assert "error" not in result, result
    assert (result["added"], result.get("news_mentions"), conn.rollbacks) == (0, 1, 0)
    assert _verdicts(beats) == [(SOURCE_ID, "success", 0, None)]


def test_a_cycle_that_ran_infrastructure_sync_leaves_rows_unmeasured(
        brain_mod, brain, beats, monkeypatch):
    # Every 10th cycle sync writes rows through infrastructure_discovery and
    # swallows its errors into zeros, so its counts cannot join the sum.
    brain.state["total_cycles"] = 10
    monkeypatch.setattr(brain, "sync_infrastructure",
                        lambda: _shape(brain_mod, "sync_infrastructure", fiber=4))
    _run(brain_mod, brain, monkeypatch,
         capacity=_shape(brain_mod, "capacity", new_pipeline=2))
    assert _verdicts(beats) == [(SOURCE_ID, "success", None, None)]


def test_a_cycle_that_raises_beats_one_failure_naming_only_the_type(
        brain_mod, brain, beats, monkeypatch):
    def save_state():
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(brain, "_save_state", save_state)
    with pytest.raises(RuntimeError, match=SENTINEL):
        _run(brain_mod, brain, monkeypatch)
    assert _verdicts(beats) == [
        (SOURCE_ID, "failure", None, "cycle raised RuntimeError")]
    assert isinstance(beats[0]["duration_ms"], int)


def test_row_tallies_are_exactly_the_steps_that_insert(brain_mod):
    """_HEARTBEAT_ROW_TALLIES is written by hand, so pin it to the code: a step
    that gains an INSERT (fiber, if its writes return) must join it, and each
    tally must be a counter its step really increments."""
    tree, methods = _methods(brain_mod)
    insert_sql_names = {t.id for n in tree.body if isinstance(n, ast.Assign)
                        for t in n.targets if isinstance(t, ast.Name)
                        and "INSERT INTO" in ast.unparse(n.value).upper()}

    def issues_insert(fn):
        doc = ast.get_docstring(fn, clean=False)
        return any(
            (isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value != doc and "INSERT INTO" in n.value.upper())
            or (isinstance(n, ast.Name) and n.id in insert_sql_names)
            for n in ast.walk(fn))

    steps = _steps(brain_mod)
    tallies = dict(brain_mod.AutonomousBrain._HEARTBEAT_ROW_TALLIES)
    assert {k for k, m in steps.items() if issues_insert(methods[m])} == set(tallies)
    for key, tally in tallies.items():
        assert any(isinstance(n, ast.AugAssign) and _results_subscript(n.target, tally)
                   for n in ast.walk(methods[steps[key]])), (key, tally)
